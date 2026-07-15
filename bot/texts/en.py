"""All user-facing strings — English. Must mirror the key set of ru.py."""

from __future__ import annotations

import html
import re

# --- /start + main menu ---
START_GREETING = (
    "Hello! This is GLPIgram — the tech-support bot.\n"
    "Press “🆕 New ticket” to create a ticket, or “📋 My tickets” to see yours."
)
# Reply-keyboard buttons (persistent main menu).
BTN_NEW_TICKET = "🆕 New ticket"
BTN_MY_TICKETS = "📋 My tickets"
BTN_STATS = "📊 Statistics"  # tech-only menu button -> /stats

# --- free-text outside a dialog ---
FREETEXT_OFFER = "Create a ticket with this text as the description?"
BTN_CREATE_TICKET = "🆕 Create"

# --- bot command descriptions (set_my_commands) ---
CMD_NEW_DESCRIPTION = "Create a new ticket"
CMD_TICKETS_DESCRIPTION = "My tickets"

# --- account linking (feature 2) ---
LINK_WELCOME = (
    "Hi! This is GLPIgram — the tech-support bot.\n"
    "To create tickets you need to sign in — send your work login "
    "(as for Windows sign-in) or your first and last name."
)
LINK_ASK_LOGIN = "Send your work login (as for Windows sign-in):"
LINK_USER_NOT_FOUND = (
    "No active GLPI user with that login was found.\nCheck the spelling and send the login again."
)
LINK_NAME_NOT_FOUND = (
    "No active users with that name were found.\nSend your work login (as for Windows sign-in)."
)
LINK_NAME_PICK_MANY = "Several users were found. Pick yourself:"

# Buttons for the name-based candidate step.
BTN_LINK_ITS_ME = "✅ That's me"
BTN_LINK_NOT_ME = "❌ Not me"
BTN_LINK_NONE_OF_THESE = "I'm not on the list"
LINK_NO_TECH_GROUP = (
    "Linking is temporarily unavailable: the support group is not configured. "
    "Please contact your administrator."
)
LINK_PENDING = "Your linking request has been sent. Please wait for support to confirm it."
LINK_CONFIRMED = "✅ Account linked. You can now create tickets."
LINK_REJECTED = "❌ Your linking request was rejected. Please contact support."

# Shown to a user who tries to do anything before linking.
NEED_LINK = "Link your account first: send /start."

# Buttons on the tech-group confirmation card.
BTN_LINK_CONFIRM = "✅ Confirm"
BTN_LINK_REJECT = "❌ Reject"

# Toasts / admin replies.
TECH_ONLY = "This command is available to support technicians only."
CB_TECH_GROUP_ONLY = "Linking can only be confirmed from the support group."
LINK_ALREADY_HANDLED = "This request has already been handled."
ADMIN_LINK_USAGE = (
    "Usage: reply to the user's message with <code>/link &lt;login&gt;</code>\n"
    "or give a tg_id: <code>/link &lt;tg_id&gt; &lt;login&gt;</code>."
)
ADMIN_UNLINK_USAGE = "Usage: <code>/unlink &lt;login&gt;</code>."


def link_name_pick_one(glpi_name: str) -> str:
    return f"Found: <b>{html.escape(glpi_name)}</b>. Is that you?"


def link_request(*, tg_id: int, tg_name: str, login: str, glpi_name: str, glpi_id: int) -> str:
    return (
        "🔗 <b>Account linking request</b>\n\n"
        f"Telegram: {html.escape(tg_name)} (id <code>{tg_id}</code>)\n"
        f"GLPI: <b>{html.escape(glpi_name)}</b> "
        f"(login <code>{html.escape(login)}</code>, id {glpi_id})\n\n"
        "Confirm the link?"
    )


def link_request_resolved(*, glpi_name: str, login: str, approved: bool, by: str) -> str:
    head = "✅ Link confirmed" if approved else "❌ Link rejected"
    return (
        f"{head}\n{html.escape(glpi_name)} (login <code>{html.escape(login)}</code>)\n"
        f"Handled by: {html.escape(by)}"
    )


def admin_link_ok(*, tg_id: int, glpi_name: str, login: str) -> str:
    return (
        f"✅ Linked: id <code>{tg_id}</code> → {html.escape(glpi_name)} "
        f"(login <code>{html.escape(login)}</code>)."
    )


def admin_unlink_result(*, login: str, removed: bool) -> str:
    if removed:
        return f"✅ The link for login <code>{html.escape(login)}</code> has been removed."
    return f"No active link found for login <code>{html.escape(login)}</code>."


# --- /new dialog ---
NEW_CHOOSE_CATEGORY = "Choose a ticket category:"
NEW_NO_CATEGORIES = (
    "Failed to load categories from GLPI. Try again later or contact your administrator."
)
NEW_CHOOSE_URGENCY = "Choose the urgency:"
NEW_ENTER_TITLE = "Enter a short ticket title:"
NEW_TITLE_TOO_LONG = "The title is too long (250 characters max). Enter a shorter one:"
NEW_ENTER_DESCRIPTION = "Describe the problem in more detail:"
NEW_CONFIRM_HEADER = "Review the ticket before submitting:"
NEW_CREATING = "Creating the ticket…"
NEW_CANCELLED = "Ticket creation cancelled."
NEW_EXPECT_TEXT = "Please send text."

# --- attachments (feature 6) ---
NEW_ATTACH_PROMPT = "You can attach photos or documents (one per message), then press “Done”."
BTN_ATTACH_DONE = "✅ Done"
# Confirmation shown when the user taps Cancel during the attachments step.
ATTACH_CANCEL_CONFIRM = "Cancel ticket creation?"
BTN_ATTACH_CANCEL_YES = "Yes, cancel"
BTN_ATTACH_CANCEL_NO = "No"
# Case-insensitive text that also finishes the attachments step (keyboard fallback).
ATTACH_DONE_WORD = "done"
ATTACH_TOO_LARGE = "The file is too large (20 MB max). Send a smaller one."
ATTACH_TOO_MANY = "Attachment limit reached. Press “Done”."
ATTACH_UNSUPPORTED = "Send a photo/document or press “Done”."
COMMENT_ATTACHMENT_PLACEHOLDER = "(attachment)"


def attach_added(count: int) -> str:
    return f"📎 Attachment added ({count} total). Send more or press “Done”."


def attachments_partial_failure(uploaded: int, total: int) -> str:
    return f"⚠️ Uploaded {uploaded} of {total} attachments. The rest could not be attached."


# --- urgency labels ---
URGENCY_LOW_LABEL = "🟢 Low"
URGENCY_MEDIUM_LABEL = "🟡 Medium"
URGENCY_HIGH_LABEL = "🔴 High"
# Dedicated "prod" level (GLPI urgency 5): the only one that breaks quiet hours.
URGENCY_URGENT_LABEL = "🔴 Urgent (prod)"
# Explicit banner on the tech-group card for an urgent (prod) ticket.
URGENT_CARD_MARK = "🔴 <b>URGENT (prod)</b>"

# Full GLPI urgency scale (1..5) for cards; the /new dialog exposes only three
# ordinary levels plus the urgent (prod) level (mapped to 5).
_URGENCY_SCALE = {
    1: ("⚪", "very low"),
    2: ("🟢", "low"),
    3: ("🟡", "medium"),
    4: ("🔴", "high"),
    5: ("🚨", "very high"),
}


def urgency_line(urgency: int) -> str:
    """Detail-view line like "🔴 Urgency: high"; tolerant of unknown values."""
    scale = _URGENCY_SCALE.get(urgency)
    if scale is None:
        return f"Urgency: {urgency}"
    emoji, name = scale
    return f"{emoji} Urgency: {name}"


def urgency_card_line(urgency: int) -> str:
    """Tech-card headline like "🟡 <b>Medium urgency</b>".

    The urgent (prod) level gets an explicit, unmistakable banner instead of the
    generic scale wording — it is the loudest priority signal on the card.
    """
    if urgency == 5:  # URGENCY_URGENT (prod) — dedicated breakthrough level
        return URGENT_CARD_MARK
    scale = _URGENCY_SCALE.get(urgency)
    if scale is None:
        return f"Urgency: {urgency}"
    emoji, name = scale
    return f"{emoji} <b>{name.capitalize()} urgency</b>"


# --- buttons ---
BTN_CONFIRM = "✅ Submit"
BTN_CANCEL = "❌ Cancel"

# --- errors / fallbacks ---
STATS_TECH_ONLY = "📊 Statistics are available to technicians only."
STATS_USERS_UNAVAILABLE = "👥 User statistics are temporarily unavailable."
BTN_TECH_TICKETS = "👨\u200d💻 In progress"  # tech menu: tickets assigned to me
TECH_TICKETS_EMPTY = "No active tickets are assigned to you."

GLPI_ERROR = (
    "An error occurred while talking to GLPI. The ticket was not created — try again later."
)
GENERIC_ERROR = "Something went wrong. Please try again."
STALE_BUTTON = "This button has expired. Open the menu again."
USE_BUTTONS = "Please use the buttons above."


def ticket_created(ticket_id: int, url: str | None) -> str:
    return f"✅ Ticket {_ticket_ref(ticket_id, url)} created."


def urgency_label(urgency: int) -> str:
    from ..glpi.client import URGENCY_HIGH, URGENCY_LOW, URGENCY_MEDIUM, URGENCY_URGENT

    return {
        URGENCY_LOW: URGENCY_LOW_LABEL,
        URGENCY_MEDIUM: URGENCY_MEDIUM_LABEL,
        URGENCY_HIGH: URGENCY_HIGH_LABEL,
        URGENCY_URGENT: URGENCY_URGENT_LABEL,
    }.get(urgency, str(urgency))


def confirm_summary(
    category_name: str, urgency: int, title: str, description: str, attachments: int = 0
) -> str:
    lines = (
        f"{NEW_CONFIRM_HEADER}\n\n"
        f"<b>Category:</b> {html.escape(category_name)}\n"
        f"<b>Urgency:</b> {urgency_label(urgency)}\n"
        f"<b>Title:</b> {html.escape(title)}\n"
        f"<b>Description:</b>\n{html.escape(description)}"
    )
    if attachments:
        lines += f"\n<b>Attachments:</b> {attachments}"
    return lines


# --- sync loop notifications (feature 4) ---
# GLPI ticket status ids (see glpi/client.py TICKET_STATUS_*).
_STATUS_LABELS = {
    1: "🆕 New",
    2: "⚙️ Processing (assigned)",
    3: "⚙️ Processing (planned)",
    4: "⏸ Pending",
    5: "✅ Solved",
    6: "🔒 Closed",
}

# Tech-group notification buttons (feature 5).
BTN_TECH_TAKE = "🙋 Take the ticket"  # full-width row -> length is fine
OPEN_IN_GLPI = "Open in GLPI"


def attachments_note(count: int) -> str:
    """Card line: the requester attached N files (see the GLPI link)."""
    return f"📎 Attachments: {count}"


def attachments_via_link(filenames: list[str], url: str) -> str:
    """Fallback line when a followup's files are too large to upload to Telegram."""
    names = ", ".join(html.escape(n) for n in filenames)
    return f'📎 Files too large for Telegram: {names}. Open them in <a href="{url}">GLPI</a>.'


BTN_TECH_COMMENT = "💬 Reply"
BTN_TECH_CLOSE = "✅ Close"

# --- tech actions (feature 5) ---
TECH_TAKEN_TOAST = "Ticket taken."


# The ticket number is part of the prompt: if the tech taps a second card
# before replying, the DM state is overwritten and the latest prompt is the
# only reliable statement of which ticket the reply will go to.
def tech_ask_solution(ticket_id: int) -> str:
    return f"Enter the solution text — ticket #{ticket_id} will be closed. Or press Cancel."


def tech_ask_comment(ticket_id: int) -> str:
    return f"Enter a comment for ticket #{ticket_id} — or press Cancel."


TECH_SOLUTION_DONE = "✅ Solution saved, ticket closed."
TECH_COMMENT_DONE = "💬 Comment added."
TECH_EXPECT_TEXT = "Please send text."
DIALOG_CANCELLED = "❌ Cancelled."
# Shown as a toast when the bot can't DM the technician (they never opened it).
TECH_DM_FAILED = "Open a private chat with the bot (/start) and try again."


def tech_card_taken(name: str) -> str:
    return f"🙋 In progress: {html.escape(name)}"


def tech_card_solved(name: str) -> str:
    return f"✅ Closed by: {html.escape(name)}"


# --- urgent (prod) level: warning gate on the urgency step ---
URGENT_WARNING = (
    "⚠️ This category is for urgent production-related issues.\n"
    "The team will be notified at any time of day, including nights and weekends.\n"
    "Use it with great care."
)
BTN_URGENT_CONFIRM = "✅ Confirm"
BTN_URGENT_DECLINE = "❌ Cancel"

# --- quiet hours / off-hours (feature: quiet hours) ---
QUIET_URGENT_NOTICE = (
    "The ticket is marked as urgent (prod) — the technicians have been notified "
    "and will look into it as soon as possible."
)

# Weekday names for the "on Monday" phrasing, keyed by ISO weekday.
_WEEKDAY_PREP = {
    1: "on Monday",
    2: "on Tuesday",
    3: "on Wednesday",
    4: "on Thursday",
    5: "on Friday",
    6: "on Saturday",
    7: "on Sunday",
}


def _plural_tickets(n: int) -> str:
    return "ticket" if n == 1 else "tickets"


def next_work_phrase(target, now) -> str:
    """Human phrasing for when work resumes: "today at 09:00" / "on Monday at 09:00"."""
    hm = target.strftime("%H:%M")
    if target.date() == now.date():
        return f"today at {hm}"
    return f"{_WEEKDAY_PREP[target.isoweekday()]} at {hm}"


def quiet_hours_notice(target, now) -> str:
    return (
        "🌙 It's outside working hours right now — the support team will see "
        f"your ticket {next_work_phrase(target, now)}."
    )


def deferred_batch_header(count: int) -> str:
    return f"🌅 {count} {_plural_tickets(count)} arrived outside working hours:"


_TAG_RE = re.compile(r"<[^>]+>")


def ticket_status_label(status: int) -> str:
    return _STATUS_LABELS.get(status, f"status {status}")


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


# Description shown under the title in cards / detail views (feature: content).
DESCRIPTION_LIMIT = 200


def description_block(content: str | None) -> str:
    """HTML-ready description snippet for a card/detail, or "" when empty.

    GLPI stores the body as HTML: strip tags, decode entities, cap at
    ~200 chars with an ellipsis (full text lives behind the GLPI link), then
    re-escape for the HTML parse mode. Blank/markup-only content yields "".
    """
    if not content:
        return ""
    cleaned = clean_glpi_text(content, limit=DESCRIPTION_LIMIT)
    if not cleaned:
        return ""
    return html.escape(cleaned)


def _ticket_ref(ticket_id: int, url: str | None) -> str:
    """The ticket number, clickable when the GLPI url is known (HTML mode)."""
    ref = f"#{ticket_id}"
    return f'<a href="{url}">{ref}</a>' if url else ref


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
    urgency: int | None = None,
    requester_name: str | None = None,
    requester_tg_id: int | None = None,
    attachments_count: int = 0,
    history: list[str] | None = None,
    assignee: str | None = None,
    description: str | None = None,
) -> str:
    """Tech-group card: number + urgency on top (the tech's priority signal),
    then bold title, the description snippet, author, then a named link instead
    of a bare URL. The "New" status is implied by 🆕 and shown only when it is
    something else (e.g. a deferred card flushed after the ticket was taken
    overnight)."""
    head = f"🆕 <b>Ticket #{ticket_id}</b>"
    if urgency is not None:
        head += f"\n{urgency_card_line(urgency)}"
    if status and status != 1:  # 1 = New (TICKET_STATUS_NEW)
        head += f"\nStatus: {ticket_status_label(status)}"
    if assignee:
        head += f"\n🙋 Assignee: {html.escape(assignee)}"
    # One compact body block: emoji markers for eye-scanning, no extra air.
    body = [f"📝 <b>{html.escape(title)}</b>"]
    desc = description_block(description)
    if desc:
        body.append(desc)
    if requester_name:
        body.append(f"👤 {user_mention(requester_name, requester_tg_id)}")
    if attachments_count:
        body.append(attachments_note(attachments_count))
    if url:
        body.append(f'🔗 <a href="{url}">{OPEN_IN_GLPI}</a>')
    text = head + "\n\n" + "\n".join(body)
    if history:
        text += "\n\n" + HISTORY_HEADER + "\n" + "\n".join(history)
    return text


def notify_status_change(*, ticket_id: int, title: str, status: int, url: str | None) -> str:
    return (
        f"🔔 <b>Ticket {_ticket_ref(ticket_id, url)}</b>: status changed\n"
        f"{html.escape(title)}\n"
        f"New status: {ticket_status_label(status)}"
    )


def notify_followup(*, ticket_id: int, title: str, body: str, url: str | None) -> str:
    snippet = html.escape(clean_glpi_text(body))
    return (
        f"💬 <b>New comment on ticket {_ticket_ref(ticket_id, url)}</b>\n"
        f"{html.escape(title)}\n\n"
        f"{snippet}"
    )


# --- /tickets (feature 3) ---
MY_TICKETS_HEADER = "📋 Your open tickets:"
MY_TICKETS_EMPTY = "You have no open tickets. Create one via “🆕 New ticket”."
MYT_NO_FOLLOWUPS = "No comments yet."
MYT_COMMENT_DONE = "💬 Comment added."
MYT_UNASSIGNED = "unassigned"

BTN_MYT_COMMENT = "💬 Add comment"
BTN_MYT_CLOSE = "✅ Close ticket"
BTN_MYT_REMIND = "🔔 Send a reminder"
BTN_MYT_BACK = "⬅️ Back to list"
# On its own row in the close prompt (full width) — length is not a concern.
BTN_MYT_CLOSE_NO_COMMENT = "Close without a comment"

MYT_CLOSE_PROMPT = "Write the reason for closing, close without a comment — or press Cancel."
MYT_CLOSE_DONE = "✅ Ticket closed."
MYT_REMIND_SENT = "🔔 Reminder sent."
# Toast when the ticket was taken between opening the detail and tapping remind.
MYT_REMIND_NOT_NEW = "The ticket is already being worked on — no reminder needed."


def myt_remind_cooldown(hours_left: int) -> str:
    return f"A reminder has already been sent. You can send another in {hours_left} h."


def notify_reminder(*, ticket_id: int, title: str, hours_ago: int | None) -> str:
    age = f" (created {hours_ago} h ago)" if hours_ago is not None else ""
    head = f"🔔 <b>The requester is reminding about ticket #{ticket_id}:</b>"
    return f"{head}\n{html.escape(title)}{age}"


def close_followup_body(name: str, reason: str) -> str:
    """Followup text recorded in GLPI when the requester closes the ticket."""
    return f"{name} closed the ticket.\nReason: {reason}"


def notify_closed_by_requester(*, ticket_id: int, reason: str | None, assignees: list[str]) -> str:
    if reason:
        text = (
            f"🔒 <b>Ticket #{ticket_id} was closed by the requester.</b>\n"
            f"Reason: {html.escape(clean_glpi_text(reason, limit=500))}"
        )
    else:
        text = f"🔒 <b>Ticket #{ticket_id} was closed by the requester without a comment.</b>"
    if assignees:
        names = ", ".join(html.escape(a) for a in assignees)
        text += f"\nWas assigned to: {names}"
    return text


def btn_open_ticket(ticket_id: int, title: str) -> str:
    """Short label for a per-ticket button in the list."""
    short = title if len(title) <= 30 else title[:29] + "…"
    return f"#{ticket_id} · {short}"


def myt_ask_comment(ticket_id: int) -> str:
    return f"Enter a comment for ticket #{ticket_id} — or press Cancel."


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
    urgency: int | None = None,
    description: str | None = None,
) -> str:
    body = "\n".join(followups) if followups else MYT_NO_FOLLOWUPS
    urgency_row = f"{urgency_line(urgency)}\n" if urgency is not None else ""
    desc = description_block(description)
    desc_row = f"{desc}\n\n" if desc else "\n"
    return (
        f"<b>Ticket {_ticket_ref(ticket_id, url)}</b>\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{desc_row}"
        f"Status: {ticket_status_label(status)}\n"
        f"{urgency_row}"
        f"{_assignee_line(assignee)}\n\n"
        f"<b>Recent comments:</b>\n{body}"
    )


def followup_line(author: str | None, body: str) -> str:
    snippet = html.escape(clean_glpi_text(body, limit=300))
    if author:
        return f"• <b>{html.escape(author)}:</b> {snippet}"
    return f"• {snippet}"


# --- living card history (single evolving card in the tech group) ---
HISTORY_HEADER = "── History ──"


def hist_taken(name: str) -> str:
    return f"🙋 Taken by: {html.escape(name)}"


def hist_comment(author: str | None) -> str:
    return f"💬 Comment ({html.escape(author)})" if author else "💬 Comment"


def hist_status(status: int) -> str:
    return f"🔄 {ticket_status_label(status)}"


def hist_closed_by_requester() -> str:
    return "🔒 Closed by the requester"


def reply_new_comment(ticket_id: int) -> str:
    return f"💬 New comment on ticket #{ticket_id}"


def solved_notice(*, ticket_id: int, tech_name: str | None, solution: str) -> str:
    """Requester notification carrying the actual solution text."""
    body = html.escape(clean_glpi_text(solution, limit=800))
    who = f" — {html.escape(tech_name)}" if tech_name else ""
    return f"✅ Your ticket #{ticket_id} has been solved{who}: {body}"


# --- ITIL solution cycle: solved -> requester confirms or returns to work ---
BTN_CONFIRM_SOLUTION = "✅ Confirm"
BTN_RETURN_TO_WORK = "↩️ Return to work"
# Full-width row on a solved card; length is fine.
BTN_TECH_WAITING = "⏳ Solved, awaiting confirmation"
WAITING_TOAST = "A solution has been proposed — waiting for the requester to confirm."
RETURNED_ACK = "↩️ The ticket has been returned to work."


def solution_proposed(*, ticket_id: int, tech_name: str | None, solution: str) -> str:
    """Requester prompt: the solution text + confirm/return buttons follow."""
    who = f" — {html.escape(tech_name)}" if tech_name else ""
    head = f"✅ A solution has been proposed for ticket #{ticket_id}{who}"
    body = html.escape(clean_glpi_text(solution, limit=800)) if solution else ""
    if body:
        head += f": {body}"
    return head + "\n\nIs the problem solved?"


def closed_thanks(ticket_id: int) -> str:
    return f"Ticket #{ticket_id} is closed, thank you!"


def ask_return_reason(ticket_id: int) -> str:
    return f"Describe what is still unresolved in ticket #{ticket_id} — or press Cancel."


def returned_to_work(ticket_id: int, reason: str) -> str:
    body = html.escape(clean_glpi_text(reason, limit=500))
    return f"↩️ The requester returned ticket #{ticket_id} to work: {body}"


def reply_confirmed(ticket_id: int) -> str:
    return f"👍 The requester confirmed the solution of ticket #{ticket_id}"


def hist_solved(name: str) -> str:
    return f"✅ Solution proposed: {html.escape(name)}"


def hist_confirmed() -> str:
    return "👍 Requester confirmed the solution"


def hist_returned() -> str:
    return "↩️ Returned to work by the requester"


# --- unassigned-tickets reminder (tech group) ---
UNASSIGNED_HEADER = "⚠️ <b>Unassigned tickets:</b>"


def unassigned_line(ticket_id: int, title: str, hours: int) -> str:
    short = title if len(title) <= 40 else title[:39] + "…"
    return f"#{ticket_id} “{html.escape(short)}” ({hours}h)"


def btn_take_ticket(ticket_id: int) -> str:
    return f"🙋 Take #{ticket_id}"


def stats_summary(counts: dict[int, int]) -> str:
    """Open-queue breakdown for /stats; ``counts`` maps GLPI status -> amount."""
    total = sum(counts.values())
    if not total:
        return "📊 No open tickets 🎉"
    lines = "\n".join(
        f"{ticket_status_label(status)} — <b>{count}</b>"
        for status, count in sorted(counts.items())
        if count
    )
    return f"📊 <b>Open tickets: {total}</b>\n\n{lines}"


def stats_users_block(total: int, techs: int, recent: int) -> str:
    """Linked-users section of /stats (counts from the bot's own SQLite)."""
    return (
        f"👥 <b>Users</b>\n"
        f"Linked: <b>{total}</b> · technicians: <b>{techs}</b>\n"
        f"New in 7 days: <b>{recent}</b>"
    )


def tech_tickets_list(in_work: list[tuple[int, str]], waiting: list[tuple[int, str]]) -> str:
    """Tech's assigned tickets, two groups; open a ticket with the buttons below."""
    parts = ["👨\u200d💻 <b>Tickets assigned to you</b>"]
    if in_work:
        parts.append(
            "⚙️ In progress:\n"
            + "\n".join(f"• #{tid} — {html.escape(title)}" for tid, title in in_work)
        )
    if waiting:
        parts.append(
            "⏳ Awaiting confirmation:\n"
            + "\n".join(f"• #{tid} — {html.escape(title)}" for tid, title in waiting)
        )
    return "\n\n".join(parts)


# --- handoff (reassign the ticket to another technician) ---
BTN_HANDOFF = "🔄 Reassign"
HANDOFF_NO_TECHS = "Nobody to hand over to: no technicians are linked in the bot."
HANDOFF_TARGET_GONE = "This technician is no longer linked to the bot."
HANDOFF_CANCELLED = "🔄 Reassignment cancelled."


def handoff_pick(ticket_id: int) -> str:
    return f"🔄 Who should take over ticket #{ticket_id}?"


def handoff_done(ticket_id: int, name: str) -> str:
    return f"🔄 Ticket #{ticket_id} handed over to {html.escape(name)}."


def handoff_to_new(ticket_id: int, title: str, urgency: int | None) -> str:
    line = f"🔄 Ticket #{ticket_id} has been reassigned to you: {html.escape(title)}"
    if urgency is not None:
        line += f", urgency {urgency_label(urgency)}"
    return line


def handoff_to_requester(ticket_id: int, name: str) -> str:
    return f"🔄 Your ticket #{ticket_id} is now handled by {html.escape(name)}"


def hist_handoff(frm: str | None, to: str) -> str:
    return f"🔄 Handed over: {html.escape(frm or '—')} → {html.escape(to)}"
