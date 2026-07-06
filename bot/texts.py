"""All user-facing strings (Russian). No i18n framework by design."""

from __future__ import annotations

import html
import re

# --- /start + main menu ---
START_GREETING = (
    "Здравствуйте! Я бот службы поддержки.\n"
    "Создавайте заявки и следите за их статусом прямо здесь.\n\n"
    "Выберите действие на клавиатуре ниже или отправьте команду /new."
)
# Reply-keyboard buttons (persistent main menu).
BTN_NEW_TICKET = "🆕 Новая заявка"
BTN_MY_TICKETS = "📋 Мои заявки"

# --- free-text outside a dialog ---
FREETEXT_OFFER = "Создать заявку с этим текстом в качестве описания?"
BTN_CREATE_TICKET = "🆕 Создать"

# --- bot command descriptions (set_my_commands) ---
CMD_NEW_DESCRIPTION = "Создать новую заявку"
CMD_TICKETS_DESCRIPTION = "Мои заявки"

# --- account linking (feature 2) ---
LINK_WELCOME = (
    "Здравствуйте! Я бот службы поддержки.\n"
    "Чтобы начать работу, привяжите ваш рабочий аккаунт.\n\n"
    "Отправьте ваши фамилию и имя или рабочий логин (как при входе в Windows)."
)
LINK_ASK_LOGIN = "Отправьте ваш рабочий логин (как при входе в Windows):"
LINK_USER_NOT_FOUND = (
    "Не нашёл активного пользователя с таким логином в GLPI.\n"
    "Проверьте написание и отправьте логин ещё раз."
)
LINK_NAME_NOT_FOUND = (
    "Не нашёл активных пользователей с таким именем.\n"
    "Отправьте ваш рабочий логин (как при входе в Windows)."
)
LINK_NAME_PICK_MANY = "Нашлось несколько пользователей. Выберите себя:"

# Buttons for the name-based candidate step.
BTN_LINK_ITS_ME = "✅ Это я"
BTN_LINK_NOT_ME = "❌ Это не я"
BTN_LINK_NONE_OF_THESE = "Меня нет в списке"
LINK_NO_TECH_GROUP = (
    "Привязка временно недоступна: не настроена группа технической поддержки. "
    "Обратитесь к администратору."
)
LINK_PENDING = "Заявка на привязку отправлена. Ожидайте подтверждения от технической поддержки."
LINK_CONFIRMED = "✅ Аккаунт привязан. Теперь можно создавать заявки."
LINK_REJECTED = "❌ Запрос на привязку отклонён. Обратитесь к технической поддержке."

# Shown to a user who tries to do anything before linking.
NEED_LINK = "Сначала привяжите аккаунт: отправьте /start."

# Buttons on the tech-group confirmation card.
BTN_LINK_CONFIRM = "✅ Подтвердить"
BTN_LINK_REJECT = "❌ Отклонить"

# Toasts / admin replies.
TECH_ONLY = "Команда доступна только техническим специалистам."
CB_TECH_GROUP_ONLY = "Подтверждать привязку можно только в группе поддержки."
LINK_ALREADY_HANDLED = "Этот запрос уже обработан."
ADMIN_LINK_USAGE = (
    "Использование: ответьте на сообщение пользователя командой "
    "<code>/link &lt;логин&gt;</code>\n"
    "или укажите tg_id: <code>/link &lt;tg_id&gt; &lt;логин&gt;</code>."
)
ADMIN_UNLINK_USAGE = "Использование: <code>/unlink &lt;логин&gt;</code>."


def link_name_pick_one(glpi_name: str) -> str:
    return f"Нашёл: <b>{html.escape(glpi_name)}</b>. Это вы?"


def link_request(*, tg_id: int, tg_name: str, login: str, glpi_name: str, glpi_id: int) -> str:
    return (
        "🔗 <b>Запрос на привязку аккаунта</b>\n\n"
        f"Telegram: {tg_name} (id <code>{tg_id}</code>)\n"
        f"GLPI: <b>{glpi_name}</b> (логин <code>{login}</code>, id {glpi_id})\n\n"
        "Подтвердить привязку?"
    )


def link_request_resolved(*, glpi_name: str, login: str, approved: bool, by: str) -> str:
    head = "✅ Привязка подтверждена" if approved else "❌ Привязка отклонена"
    return f"{head}\n{glpi_name} (логин <code>{login}</code>)\nОбработал: {by}"


def admin_link_ok(*, tg_id: int, glpi_name: str, login: str) -> str:
    return f"✅ Привязано: id <code>{tg_id}</code> → {glpi_name} (логин <code>{login}</code>)."


def admin_unlink_result(*, login: str, removed: bool) -> str:
    if removed:
        return f"✅ Привязка для логина <code>{login}</code> удалена."
    return f"Активная привязка для логина <code>{login}</code> не найдена."


# --- /new dialog ---
NEW_CHOOSE_CATEGORY = "Выберите категорию заявки:"
NEW_NO_CATEGORIES = (
    "Не удалось загрузить категории из GLPI. Попробуйте позже или обратитесь к администратору."
)
NEW_CHOOSE_URGENCY = "Выберите срочность:"
NEW_ENTER_TITLE = "Введите короткий заголовок заявки:"
NEW_TITLE_TOO_LONG = "Заголовок слишком длинный (максимум 250 символов). Введите короче:"
NEW_ENTER_DESCRIPTION = "Опишите проблему подробнее:"
NEW_CONFIRM_HEADER = "Проверьте заявку перед отправкой:"
NEW_CREATING = "Создаю заявку…"
NEW_CANCELLED = "Создание заявки отменено."
NEW_EXPECT_TEXT = "Пожалуйста, отправьте текст."

# --- attachments (feature 6) ---
NEW_ATTACH_PROMPT = (
    "Можете прикрепить фото или документы (по одному сообщению), затем нажмите «Готово»."
)
BTN_ATTACH_DONE = "✅ Готово"
# Confirmation shown when the user taps Отмена during the attachments step.
ATTACH_CANCEL_CONFIRM = "Отменить создание заявки?"
BTN_ATTACH_CANCEL_YES = "Да, отменить"
BTN_ATTACH_CANCEL_NO = "Нет"
# Case-insensitive text that also finishes the attachments step (keyboard fallback).
ATTACH_DONE_WORD = "готово"
ATTACH_TOO_LARGE = "Файл слишком большой (максимум 20 МБ). Отправьте файл поменьше."
ATTACH_TOO_MANY = "Достигнут предел вложений. Нажмите «Готово»."
ATTACH_UNSUPPORTED = "Отправьте фото/документ или нажмите «Готово»."
COMMENT_ATTACHMENT_PLACEHOLDER = "(вложение)"


def attach_added(count: int) -> str:
    return f"📎 Вложение добавлено (всего {count}). Отправьте ещё или нажмите «Готово»."


def attachments_partial_failure(uploaded: int, total: int) -> str:
    return f"⚠️ Загружено вложений: {uploaded} из {total}. Остальные не удалось прикрепить."


# --- urgency labels ---
URGENCY_LOW_LABEL = "🟢 Низкая"
URGENCY_MEDIUM_LABEL = "🟡 Средняя"
URGENCY_HIGH_LABEL = "🔴 Высокая"

# --- buttons ---
BTN_CONFIRM = "✅ Отправить"
BTN_CANCEL = "❌ Отмена"

# --- errors / fallbacks ---
GLPI_ERROR = "Произошла ошибка при обращении к GLPI. Заявка не создана — попробуйте ещё раз позже."
GENERIC_ERROR = "Что-то пошло не так. Попробуйте ещё раз."
STALE_BUTTON = "Кнопка устарела. Откройте меню заново."
USE_BUTTONS = "Пожалуйста, воспользуйтесь кнопками выше."


def ticket_created(ticket_id: int, url: str | None) -> str:
    line = f"✅ Заявка №{ticket_id} создана."
    if url:
        line += f"\n{url}"
    return line


def urgency_label(urgency: int) -> str:
    from .glpi.client import URGENCY_HIGH, URGENCY_LOW, URGENCY_MEDIUM

    return {
        URGENCY_LOW: URGENCY_LOW_LABEL,
        URGENCY_MEDIUM: URGENCY_MEDIUM_LABEL,
        URGENCY_HIGH: URGENCY_HIGH_LABEL,
    }.get(urgency, str(urgency))


def confirm_summary(
    category_name: str, urgency: int, title: str, description: str, attachments: int = 0
) -> str:
    lines = (
        f"{NEW_CONFIRM_HEADER}\n\n"
        f"<b>Категория:</b> {category_name}\n"
        f"<b>Срочность:</b> {urgency_label(urgency)}\n"
        f"<b>Заголовок:</b> {title}\n"
        f"<b>Описание:</b>\n{description}"
    )
    if attachments:
        lines += f"\n<b>Вложений:</b> {attachments}"
    return lines


# --- sync loop notifications (feature 4) ---
# GLPI ticket status ids (see glpi/client.py TICKET_STATUS_*).
_STATUS_LABELS = {
    1: "🆕 Новая",
    2: "⚙️ В работе (назначена)",
    3: "⚙️ В работе (запланирована)",
    4: "⏸ Ожидает",
    5: "✅ Решена",
    6: "🔒 Закрыта",
}

# Tech-group notification buttons (feature 5).
BTN_TECH_TAKE = "🙋 Взять"
BTN_TECH_COMMENT = "💬 Комментарий"
BTN_TECH_CLOSE = "✅ Закрыть"

# --- tech actions (feature 5) ---
TECH_TAKEN_TOAST = "Заявка взята в работу."


# The ticket number is part of the prompt: if the tech taps a second card
# before replying, the DM state is overwritten and the latest prompt is the
# only reliable statement of which ticket the reply will go to.
def tech_ask_solution(ticket_id: int) -> str:
    return f"Введите текст решения — заявка №{ticket_id} будет закрыта:"


def tech_ask_comment(ticket_id: int) -> str:
    return f"Введите текст комментария к заявке №{ticket_id}:"


TECH_SOLUTION_DONE = "✅ Решение сохранено, заявка закрыта."
TECH_COMMENT_DONE = "💬 Комментарий добавлен."
TECH_EXPECT_TEXT = "Пожалуйста, отправьте текст."
TECH_ACTION_CANCELLED = "Действие отменено."
# Shown as a toast when the bot can't DM the technician (they never opened it).
TECH_DM_FAILED = "Откройте личный чат с ботом (/start) и повторите."


def tech_card_taken(name: str) -> str:
    return f"🙋 В работе: {html.escape(name)}"


def tech_card_solved(name: str) -> str:
    return f"✅ Закрыл: {html.escape(name)}"


# --- quiet hours / off-hours (feature: quiet hours) ---
QUIET_URGENT_NOTICE = (
    "Заявка помечена как срочная — специалисты получили уведомление "
    "и займутся ей при первой возможности."
)

# "в понедельник" etc. — weekday with the preposition, keyed by ISO weekday.
_WEEKDAY_PREP = {
    1: "в понедельник",
    2: "во вторник",
    3: "в среду",
    4: "в четверг",
    5: "в пятницу",
    6: "в субботу",
    7: "в воскресенье",
}


def _plural_tickets(n: int) -> str:
    tail = n % 100
    if 11 <= tail <= 14:
        return "заявок"
    match n % 10:
        case 1:
            return "заявка"
        case 2 | 3 | 4:
            return "заявки"
        case _:
            return "заявок"


def next_work_phrase(target, now) -> str:
    """Human phrasing for when work resumes: "сегодня в 09:00" / "в понедельник в 09:00"."""
    hm = target.strftime("%H:%M")
    if target.date() == now.date():
        return f"сегодня в {hm}"
    return f"{_WEEKDAY_PREP[target.isoweekday()]} в {hm}"


def quiet_hours_notice(target, now) -> str:
    return (
        "🌙 Сейчас нерабочее время — команда поддержки увидит вашу заявку "
        f"{next_work_phrase(target, now)}."
    )


def deferred_batch_header(count: int) -> str:
    return f"🌅 За нерабочее время поступило {count} {_plural_tickets(count)}:"


_TAG_RE = re.compile(r"<[^>]+>")


def ticket_status_label(status: int) -> str:
    return _STATUS_LABELS.get(status, f"статус {status}")


def clean_glpi_text(raw: str, *, limit: int = 1000) -> str:
    """Turn GLPI rich-text (HTML) into a trimmed plain-text snippet.

    GLPI stores followup/ticket bodies as HTML; strip tags, decode entities and
    cap the length so a forwarded comment stays a readable Telegram message.
    """
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    text = re.sub(r"(?i)</p\s*>|</div\s*>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _url_line(url: str | None) -> str:
    return f"\n{url}" if url else ""


def user_mention(name: str, tg_id: int | None) -> str:
    """A safe display name, as a clickable Telegram mention when the id is known."""
    safe = html.escape(name)
    return f'<a href="tg://user?id={tg_id}">{safe}</a>' if tg_id else safe


def notify_new_ticket(
    *,
    ticket_id: int,
    title: str,
    status: int,
    url: str | None,
    requester_name: str | None = None,
    requester_tg_id: int | None = None,
) -> str:
    author = ""
    if requester_name:
        author = f"\nАвтор: {user_mention(requester_name, requester_tg_id)}"
    return (
        f"🆕 <b>Новая заявка №{ticket_id}</b>\n"
        f"{html.escape(title)}"
        f"{author}\n"
        f"Статус: {ticket_status_label(status)}"
        f"{_url_line(url)}"
    )


def notify_status_change(*, ticket_id: int, title: str, status: int, url: str | None) -> str:
    return (
        f"🔔 <b>Заявка №{ticket_id}</b>: статус изменён\n"
        f"{html.escape(title)}\n"
        f"Новый статус: {ticket_status_label(status)}"
        f"{_url_line(url)}"
    )


def notify_followup(*, ticket_id: int, title: str, body: str, url: str | None) -> str:
    snippet = html.escape(clean_glpi_text(body))
    return (
        f"💬 <b>Новый комментарий по заявке №{ticket_id}</b>\n"
        f"{html.escape(title)}\n\n"
        f"{snippet}"
        f"{_url_line(url)}"
    )


# --- /tickets (feature 3) ---
MY_TICKETS_HEADER = "📋 Ваши открытые заявки:"
MY_TICKETS_EMPTY = "У вас нет открытых заявок. Создайте новую через «🆕 Новая заявка»."
MYT_NO_FOLLOWUPS = "Комментариев пока нет."
MYT_COMMENT_DONE = "💬 Комментарий добавлен."
MYT_UNASSIGNED = "не назначен"

BTN_MYT_COMMENT = "💬 Добавить комментарий"
BTN_MYT_CLOSE = "✅ Закрыть заявку"
BTN_MYT_REMIND = "🔔 Напомнить о себе"
BTN_MYT_BACK = "⬅️ К списку"
# On its own row in the close prompt (full width) — length is not a concern.
BTN_MYT_CLOSE_NO_COMMENT = "Закрыть без комментария"

MYT_CLOSE_PROMPT = "Напишите причину закрытия или закройте без комментария."
MYT_CLOSE_DONE = "✅ Заявка закрыта."
MYT_REMIND_SENT = "🔔 Напоминание отправлено."
# Toast when the ticket was taken between opening the detail and tapping remind.
MYT_REMIND_NOT_NEW = "Заявка уже в работе — напоминание не требуется."


def myt_remind_cooldown(hours_left: int) -> str:
    return f"Напоминание уже отправлено. Повторно можно через {hours_left} ч."


def notify_reminder(*, ticket_id: int, title: str, hours_ago: int | None) -> str:
    age = f" (создана {hours_ago} ч назад)" if hours_ago is not None else ""
    return f"🔔 <b>Заявитель напоминает о заявке №{ticket_id}:</b>\n{html.escape(title)}{age}"


def close_followup_body(name: str, reason: str) -> str:
    """Followup text recorded in GLPI when the requester closes the ticket."""
    return f"{name} закрыл(а) заявку.\nПричина: {reason}"


def notify_closed_by_requester(*, ticket_id: int, reason: str | None, assignees: list[str]) -> str:
    if reason:
        text = (
            f"🔒 <b>Заявка №{ticket_id} закрыта заявителем.</b>\n"
            f"Причина: {html.escape(clean_glpi_text(reason, limit=500))}"
        )
    else:
        text = f"🔒 <b>Заявка №{ticket_id} закрыта заявителем без комментария.</b>"
    if assignees:
        names = ", ".join(html.escape(a) for a in assignees)
        text += f"\nБыл назначен: {names}"
    return text


def btn_open_ticket(ticket_id: int, title: str) -> str:
    """Short label for a per-ticket button in the list."""
    short = title if len(title) <= 30 else title[:29] + "…"
    return f"№{ticket_id} · {short}"


def myt_ask_comment(ticket_id: int) -> str:
    return f"Введите текст комментария к заявке №{ticket_id}:"


def _assignee_line(assignee: str | None) -> str:
    return f"👤 {html.escape(assignee) if assignee else MYT_UNASSIGNED}"


def ticket_detail(
    *,
    ticket_id: int,
    title: str,
    status: int,
    assignee: str | None,
    followups: list[str],
    url: str | None = None,
) -> str:
    body = "\n".join(followups) if followups else MYT_NO_FOLLOWUPS
    return (
        f"<b>Заявка №{ticket_id}</b>\n"
        f"{html.escape(title)}\n\n"
        f"Статус: {ticket_status_label(status)}\n"
        f"{_assignee_line(assignee)}\n\n"
        f"<b>Последние комментарии:</b>\n{body}"
        f"{_url_line(url)}"
    )


def followup_line(author: str | None, body: str) -> str:
    snippet = html.escape(clean_glpi_text(body, limit=300))
    if author:
        return f"• <b>{html.escape(author)}:</b> {snippet}"
    return f"• {snippet}"
