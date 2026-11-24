"""Compare GLPI text to the displayed intake snapshot without destroying literal markup."""

from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser


def _normal(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class _Text(HTMLParser):
    _blocks = {"p", "div", "pre", "blockquote", "li", "tr", "h1", "h2", "h3"}
    _markup = _blocks | {
        "b",
        "strong",
        "em",
        "i",
        "u",
        "s",
        "span",
        "a",
        "code",
        "ul",
        "ol",
        "table",
        "tbody",
        "td",
        "font",
        "hr",
        "br",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def boundary(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._blocks:
            self.boundary()
        elif tag not in self._markup:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in self._blocks:
            self.boundary()
        elif tag not in self._markup:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)


def glpi_plain_text(content: str, *, known_plain: str = "") -> str:
    """Use known literal input when GLPI merely escapes or wraps it as HTML."""
    raw = _normal(content)
    known = _normal(known_plain)
    variants = {known, html.escape(known, quote=False), html.escape(known)}
    variants |= {html.escape(value, quote=False) for value in tuple(variants)}
    if raw in variants:
        return known_plain
    # GLPI can return an escaped HTML document rather than actual outer tags.
    decoded = html.unescape(raw)
    if re.match(r"^\s*<(?:p|div|pre|br|blockquote)\b", decoded, re.I) and not re.match(
        r"^\s*<(?:p|div|pre|br|blockquote)\b", raw, re.I
    ):
        raw = decoded
    parser = _Text()
    parser.feed(raw)
    parser.close()
    result = _normal("".join(parser.parts))
    return known_plain if result == known else result


async def refresh_intake_snapshot(repo, ticket) -> dict | None:
    """Return current metadata, bumping its version if GLPI title/body changed.

    The original description and model decision are immutable. A changed text
    requires explicit technician review again and never triggers another API call.
    Caller may hold ticket_action_lock for an action; the storage update is CAS.
    """
    row = await repo.get_ticket_intake(ticket.id)
    if row is None:
        category = next(
            (
                key
                for key, value in repo.intake_category_ids.items()
                if value == ticket.itilcategories_id
            ),
            None,
        )
        if category is None:
            return None
        # Scope recovery to the active pilot, but never stop the lifecycle of
        # metadata already saved. Verification failures must abort the caller's
        # action rather than silently authorizing a legacy mutation.
        if repo.intake_recovery_filter is not None:
            if not await repo.intake_recovery_filter(ticket.id):
                return None
        await repo.save_ticket_intake(
            ticket.id,
            {
                "title": html.unescape(ticket.name or ""),
                "original_description": glpi_plain_text(ticket.content or ""),
                "category": category,
                "category_id": ticket.itilcategories_id,
                "environment": "unknown",
                "approval_required": False,
                "reason": "",
                "model": "",
                "prompt_version": "recovered-v1",
                "status": "fallback",
                "metadata_recovered": True,
                "title_generated": False,
            },
            int(time.time()),
        )
        row = await repo.get_ticket_intake(ticket.id)
    shown = row.get("display_description", row.get("original_description", ""))
    body = glpi_plain_text(ticket.content or "", known_plain=shown)
    title = html.unescape(ticket.name or "")
    if _normal(title) != _normal(row.get("display_title", row.get("title", ""))) or (
        _normal(body) != _normal(shown)
    ):
        await repo.refresh_ticket_intake_snapshot(
            ticket.id,
            expected_version=row["version"],
            snapshot={
                "title": title,
                "description": body,
                "approval_recheck": row.get("approval_recheck", False)
                or row["review_state"] == "approval",
            },
            now=int(time.time()),
        )
        row = await repo.get_ticket_intake(ticket.id)
    actual = ticket.itilcategories_id or 0
    if row["category_id"] != actual:
        key = next(
            (key for key, value in repo.intake_category_ids.items() if value == actual), "external"
        )
        await repo.update_ticket_intake(
            ticket.id,
            expected_version=row["version"],
            actor_tg_id=None,
            now=int(time.time()),
            event="external_category",
            category=key,
            category_id=actual,
            details={"from": row["category_id"], "to": actual},
        )
        row = await repo.get_ticket_intake(ticket.id)
    return row
