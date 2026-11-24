"""Queue warnings describe a snapshot, without promising response times or order."""

from __future__ import annotations


def _choose(ru: str, en: str) -> str:
    from . import _lang

    return en if _lang == "en" else ru


def _tickets_ru(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "заявок"
    return "заявка" if count % 10 == 1 else "заявки" if 2 <= count % 10 <= 4 else "заявок"


def notice(
    *, queue_count: int, urgent_unassigned_count: int, urgent_assigned_count: int
) -> str | None:
    paragraphs = []
    if queue_count >= 3:
        header = (
            _choose("🟠 Сейчас большая очередь в поддержку.", "🟠 The support queue is large.")
            if queue_count >= 6
            else _choose(
                "🟡 В поддержке накопилась очередь.",
                "🟡 Support has a backlog of requests.",
            )
        )
        paragraphs.append(
            header
            + _choose(
                f" В очереди ещё {queue_count} {_tickets_ru(queue_count)}. "
                "Ответ может занять больше времени.",
                f" There are {queue_count} other tickets in the queue. A response may take longer.",
            )
        )
    if urgent_unassigned_count:
        paragraphs.append(
            _choose(
                f"🚨 Аварийных заявок без исполнителя: {urgent_unassigned_count}.",
                f"🚨 Unassigned production incident tickets: {urgent_unassigned_count}.",
            )
        )
    if urgent_assigned_count:
        paragraphs.append(
            _choose(
                f"🚨 Аварийных заявок, назначенных техникам: {urgent_assigned_count}. "
                "Обычные обращения могут обрабатываться дольше.",
                f"🚨 Production incident tickets assigned to technicians: {urgent_assigned_count}. "
                "Routine requests may take longer to handle.",
            )
        )
    return "\n\n".join(paragraphs) or None
