"""All user-facing strings (Russian). No i18n framework by design."""

from __future__ import annotations

# --- /start + main menu ---
START_GREETING = (
    "Здравствуйте! Я бот службы поддержки.\n"
    "Создавайте заявки и следите за их статусом прямо здесь.\n\n"
    "Выберите действие на клавиатуре ниже или отправьте команду /new."
)
# Reply-keyboard buttons (persistent main menu).
BTN_NEW_TICKET = "🆕 Новая заявка"
BTN_MY_TICKETS = "📋 Мои заявки"
# Feature 3 placeholder until "my tickets" is implemented.
MY_TICKETS_SOON = "Раздел «Мои заявки» скоро появится."

# --- free-text outside a dialog ---
FREETEXT_OFFER = "Создать заявку с этим текстом в качестве описания?"
BTN_CREATE_TICKET = "🆕 Создать заявку"

# --- bot command descriptions (set_my_commands) ---
CMD_NEW_DESCRIPTION = "Создать новую заявку"
CMD_TICKETS_DESCRIPTION = "Мои заявки"

# --- account linking (feature 2) ---
LINK_WELCOME = (
    "Здравствуйте! Я бот службы поддержки.\n"
    "Чтобы начать работу, привяжите ваш рабочий аккаунт.\n\n"
    "Отправьте ваш рабочий логин (тот, под которым вы входите в Windows)."
)
LINK_ASK_LOGIN = "Отправьте ваш рабочий логин (как при входе в Windows):"
LINK_USER_NOT_FOUND = (
    "Не нашёл активного пользователя с таким логином в GLPI.\n"
    "Проверьте написание и отправьте логин ещё раз."
)
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

# --- urgency labels ---
URGENCY_LOW_LABEL = "🟢 Низкая"
URGENCY_MEDIUM_LABEL = "🟡 Средняя"
URGENCY_HIGH_LABEL = "🔴 Высокая"

# --- buttons ---
BTN_CONFIRM = "✅ Отправить"
BTN_CANCEL = "❌ Отмена"

# --- errors ---
GLPI_ERROR = "Произошла ошибка при обращении к GLPI. Заявка не создана — попробуйте ещё раз позже."


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


def confirm_summary(category_name: str, urgency: int, title: str, description: str) -> str:
    return (
        f"{NEW_CONFIRM_HEADER}\n\n"
        f"<b>Категория:</b> {category_name}\n"
        f"<b>Срочность:</b> {urgency_label(urgency)}\n"
        f"<b>Заголовок:</b> {title}\n"
        f"<b>Описание:</b>\n{description}"
    )
